<#
.SYNOPSIS
  Run the 20Seconds stack in Docker.

.EXAMPLE
  .\run.ps1          # build and start in the background
  .\run.ps1 -Logs    # follow the logs
  .\run.ps1 -Down    # stop and remove the containers (data is kept in ./data)
  .\run.ps1 -Port 9000
#>
param(
    [switch]$Down,
    [switch]$Logs,
    [int]$Port = 8000
)

# Not "Stop": Windows PowerShell treats docker's stderr progress output as errors.
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker was not found on PATH. Install Docker Desktop first."
}

if ($Down) { docker compose down; exit $LASTEXITCODE }
if ($Logs) { docker compose logs -f; exit $LASTEXITCODE }

New-Item -ItemType Directory -Force data, secrets | Out-Null

# First run: create the admin secret with a random password (UTF-8, no BOM).
$secret = Join-Path $PSScriptRoot "secrets\admin.json"
if (-not (Test-Path $secret)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', ''
    $json = @{ username = "admin"; password = $password } | ConvertTo-Json
    [IO.File]::WriteAllText($secret, $json, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Created $secret (random admin password, read it from that file)."
}

$env:PORT = $Port
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Admin dashboard: http://localhost:$Port" -ForegroundColor Green
Write-Host "API docs:        http://localhost:$Port/docs"
Write-Host "Logs: .\run.ps1 -Logs    Stop: .\run.ps1 -Down"
