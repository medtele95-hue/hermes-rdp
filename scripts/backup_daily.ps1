# HERMES — daily backup (BLOC 11d, lesson of the dead-server outage)
# 1) Dated local copy of the precious files (data JSONL, .env, reports)
# 2) git push (all branches + tags) to the private remote when configured
# Registered as a Windows scheduled task (see docs/BACKUP_SIMO.md).

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp = Get-Date -Format "yyyy-MM-dd"
$target = Join-Path $repo "backups\$stamp"
New-Item -ItemType Directory -Force $target | Out-Null

$logFile = Join-Path $repo "backups\backup.log"
function Write-Log($msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -Path $logFile -Value $line -Encoding utf8
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

# -- 2) git push to the private remote (if configured) ------------------------
$git = "C:\Tools\MinGit\cmd\git.exe"
if (-not (Test-Path $git)) { $git = "git" }
$remotes = & $git remote 2>$null
if ($remotes -contains "origin") {
    & $git push origin --all 2>&1 | ForEach-Object { Write-Log "git: $_" }
    & $git push origin --tags 2>&1 | ForEach-Object { Write-Log "git: $_" }
    Write-Log "git push done"
} else {
    Write-Log "git push SKIPPED: no 'origin' remote configured (see docs/BACKUP_SIMO.md step 1)"
}

# -- 3) prune dated copies older than 30 days ---------------------------------
Get-ChildItem (Join-Path $repo "backups") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "^\d{4}-\d{2}-\d{2}$" -and $_.CreationTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue

Write-Log "backup finished"
