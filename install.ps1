$ErrorActionPreference = "Stop"

if (!(Test-Path ".\venv")) {
    python -m venv venv
}

.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

Write-Host "Hermes backend dependencies installed."
