# HERMES — daily backup (BLOC 11d, lesson of the dead-server outage;
# renforce mission4 AUTOMATION.md script 2)
# 1) Dated local copy of the precious files (data JSONL, .env, reports)
# 2) Mirror the same dated copy to Google Drive if a synced folder is found
# 3) git push (all branches + tags) to the private remote when configured
# Registered as a Windows scheduled task HERMES_DAILY_BACKUP, 09:00 daily
# (see docs/BACKUP_SIMO.md).

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp = Get-Date -Format "yyyy-MM-dd"
$target = Join-Path $repo "backups\$stamp"
New-Item -ItemType Directory -Force $target | Out-Null

$logFile = Join-Path $repo "backups\backup.log"
function Write-Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    for ($i = 0; $i -lt 5; $i++) {
        try {
            Add-Content -Path $logFile -Value $line -Encoding utf8 -ErrorAction Stop
            break
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    Write-Output $line
}

Write-Log "backup started target=$target"

# -- 1) dated copies ----------------------------------------------------------
try {
    New-Item -ItemType Directory -Force (Join-Path $target "data") | Out-Null
    Copy-Item -Path (Join-Path $repo "app\data\*.jsonl") -Destination (Join-Path $target "data") -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $repo "app\data\*.json") -Destination (Join-Path $target "data") -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $repo ".env") -Destination $target -Force -ErrorAction SilentlyContinue
    if (Test-Path (Join-Path $repo "reports")) {
        Copy-Item -Path (Join-Path $repo "reports") -Destination $target -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Log "dated copy done"
} catch {
    Write-Log "dated copy FAILED: $_"
}

# -- 2) mirror to Google Drive, if a synced folder is detected ----------------
$driveCandidates = @("G:\Mon Drive", "G:\My Drive", "$env:USERPROFILE\Mon Drive", "$env:USERPROFILE\Google Drive")
$drivePath = $driveCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$statusFile = Join-Path $repo "backups\_status.json"
if ($drivePath) {
    try {
        $driveTarget = Join-Path $drivePath "HERMES_backups\$stamp"
        New-Item -ItemType Directory -Force $driveTarget | Out-Null
        Copy-Item -Path (Join-Path $target "*") -Destination $driveTarget -Recurse -Force -ErrorAction SilentlyContinue
        Write-Log "google drive mirror done -> $driveTarget"
    } catch {
        Write-Log "google drive mirror FAILED: $_"
    }
} else {
    Write-Log "google drive SKIPPED: no synced folder found among $($driveCandidates -join ', ') - SIMO doit brancher Google Drive Desktop"
}

# -- 3) git push to the private remote (if configured) ------------------------
$git = "C:\Tools\MinGit\cmd\git.exe"
if (-not (Test-Path $git)) { $git = "git" }
$remotes = & $git remote 2>$null
$remoteOk = $false
if ($remotes -contains "origin") {
    & $git push origin --all 2>&1 | ForEach-Object { Write-Log "git: $_" }
    & $git push origin --tags 2>&1 | ForEach-Object { Write-Log "git: $_" }
    Write-Log "git push done"
    $remoteOk = $true
} else {
    Write-Log "git push SKIPPED: no 'origin' remote configured (see docs/BACKUP_SIMO.md step 1)"
}

# -- 4) prune dated copies older than 14 days ----------------------------------
Get-ChildItem (Join-Path $repo "backups") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "^\d{4}-\d{2}-\d{2}$" -and $_.CreationTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue

# -- 5) status file consumed by scripts/daily_report.py ------------------------
@{
    last_run       = (Get-Date -Format o)
    remote_ok      = $remoteOk
    google_drive   = if ($drivePath) { $drivePath } else { $null }
} | ConvertTo-Json | Set-Content -Path $statusFile -Encoding utf8

Write-Log "backup finished"
