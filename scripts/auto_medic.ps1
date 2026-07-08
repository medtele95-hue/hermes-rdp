# HERMES auto-medecin -- GRAND_PLAN_2 mission4 (2026-07-08, SIMO valide GO).
# REPARATION AUTONOME COMPLETE -- remplace la version diagnostic-seul.
#
# Securite par l'INFRASTRUCTURE (pas par permission) :
#  1. safepoint git (tag) AVANT chaque session claude -p --dangerously-skip-permissions
#  2. suite de tests COMPLETE APRES la session
#  3. UN SEUL test qui casse -> git reset --hard vers le safepoint (rollback total
#     de tout ce que la session a fait, committe ou non)
#  4. push GitHub SEULEMENT si les tests passent ET qu'il y a un vrai changement
#  5. notification Telegram de chaque ronde (RAS / repare / rollback / escalation)
#  6. journal append-only logs/auto_medic_audit.log
#  7. anti-acharnement : 3 rollbacks consecutifs -> suspension 2h + alerte CRITIQUE
#
# La ligne rouge (seuils/strategie/allowlist/risk-cap = jamais touche seul, toujours
# DECISION SIMO) est appliquee par l'IA elle-meme via les instructions de
# AUTO_MEDIC_MISSION.md -- ce script ne peut pas verifier CE QUE l'IA a change,
# seulement SI le resultat casse un test. C'est le filet de securite mecanique;
# le respect de la ligne rouge est une instruction suivie, pas une contrainte
# imposee par ce script.
#
# Tache planifiee HERMES_AUTO_MEDIC (toutes les 2h) + declenchable par
# scripts/bot_supervisor.ps1 (crash-loop) + watchdog (alerte CRITIQUE/HAUTE).
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$reportsDir = "C:\hermes-reports"
New-Item -ItemType Directory -Force $reportsDir | Out-Null
New-Item -ItemType Directory -Force (Join-Path $repo "logs") | Out-Null
$healthLog = Join-Path $reportsDir "_automation_health.log"
$stateFile = Join-Path $repo "logs\auto_medic_state.json"
$auditLog = Join-Path $repo "logs\auto_medic_audit.log"

function Write-Health($msg) {
    Add-Content -Path $healthLog -Value "$(Get-Date -Format o) [auto_medic] $msg" -Encoding utf8
}

function Write-Audit($obj) {
    $line = $obj | ConvertTo-Json -Compress
    Add-Content -Path $auditLog -Value $line -Encoding utf8
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

function Get-State {
    if (Test-Path $stateFile) {
        try { return (Get-Content $stateFile -Raw | ConvertFrom-Json) } catch { }
    }
    return [PSCustomObject]@{ rollback_streak = 0; suspended_until = $null; last_run = $null }
}

function Save-State($state) {
    $state | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding utf8
}

$state = Get-State
$now = Get-Date

if ($state.suspended_until -and ([datetime]$state.suspended_until) -gt $now) {
    Write-Health "suspended until $($state.suspended_until), skipping round"
    Write-Output "[auto_medic] SUSPENDED until $($state.suspended_until)"
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-ddTHHmmss"
$reportPath = Join-Path $reportsDir "auto_medic_$stamp.md"
$safepointTag = "safepoint-automedic-$stamp"

Write-Health "run started"
git tag $safepointTag 2>&1 | Out-Null
$preHeadHash = (git rev-parse HEAD 2>&1 | Select-Object -First 1)
Write-Health "safepoint tag=$safepointTag head=$preHeadHash"

$prompt = Get-Content (Join-Path $repo "AUTO_MEDIC_MISSION.md") -Raw

try {
    $raw = "" | & claude -p $prompt --dangerously-skip-permissions --output-format text 2>&1
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
Write-Health "claude session finished exit=$exitCode report=$reportPath"

$postHeadHash = (git rev-parse HEAD 2>&1 | Select-Object -First 1)
$hasChanges = ($postHeadHash -ne $preHeadHash) -or ((git status --short 2>&1 | Measure-Object).Count -gt 0)

$isRas = $output -match "RAS\s*[-]\s*aucun probl"

if (-not $hasChanges) {
    Write-Health "no changes made (RAS or read-only round)"
    Write-Audit @{ ts = $now.ToString("o"); outcome = if ($isRas) { "RAS" } else { "NO_CHANGE" }; report = $reportPath }
    $state.rollback_streak = 0
    $state.last_run = $now.ToString("o")
    Save-State $state
    if ($isRas) {
        Send-Telegram "HERMES auto-medecin : RAS"
    } else {
        $firstLines = ($output -split "`n" | Select-Object -First 5) -join " | "
        Send-Telegram "HERMES auto-medecin : ronde sans changement. $firstLines"
    }
    Write-Output "[auto_medic] OK (no changes) -> $reportPath"
    exit 0
}

# -- des changements existent : suite de tests complete avant de les garder --
Write-Health "changes detected, running full test suite"
$testOutput = & python -m pytest -q 2>&1
$testExit = $LASTEXITCODE
Write-Health "test suite exit=$testExit"

if ($testExit -ne 0) {
    # ROLLBACK TOTAL -- tout ce que la session a fait, committe ou non
    Write-Health "TESTS FAILED -> rollback to $safepointTag"
    git reset --hard $safepointTag 2>&1 | Out-Null
    git clean -fd 2>&1 | Out-Null
    $state.rollback_streak += 1
    $state.last_run = $now.ToString("o")

    Write-Audit @{ ts = $now.ToString("o"); outcome = "ROLLBACK"; report = $reportPath; streak = $state.rollback_streak; safepoint = $safepointTag }

    if ($state.rollback_streak -ge 3) {
        $state.suspended_until = $now.AddHours(2).ToString("o")
        Save-State $state
        Write-Health "ESCALATION streak=$($state.rollback_streak) suspended until $($state.suspended_until)"
        Send-Telegram "HERMES CRITIQUE auto-medecin : 3 tentatives echouees d'affilee (tests casses a chaque fois), suspendu 2h. Voir $reportPath"
    } else {
        Save-State $state
        Send-Telegram "HERMES auto-medecin : tentative $($state.rollback_streak)/3 echouee (tests casses), rollback applique. Voir $reportPath"
    }
    Write-Output "[auto_medic] ROLLBACK -> $reportPath"
    exit 1
}

# -- tests OK : garder les changements, pousser --
Write-Health "tests passed, pushing"
$branch = (git rev-parse --abbrev-ref HEAD 2>&1 | Select-Object -First 1)
git push origin $branch 2>&1 | Out-Null
$pushExit = $LASTEXITCODE

$state.rollback_streak = 0
$state.last_run = $now.ToString("o")
Save-State $state
Write-Audit @{ ts = $now.ToString("o"); outcome = "FIXED"; report = $reportPath; pushed = ($pushExit -eq 0) }

$firstLines = ($output -split "`n" | Select-Object -First 8) -join " | "
Send-Telegram "HERMES auto-medecin : correction appliquee et testee$(if ($pushExit -eq 0) { ' (poussee sur GitHub)' } else { ' (push echoue, verifier)' }). $firstLines"
Write-Health "run complete, pushed=$($pushExit -eq 0)"
Write-Output "[auto_medic] FIXED -> $reportPath"
