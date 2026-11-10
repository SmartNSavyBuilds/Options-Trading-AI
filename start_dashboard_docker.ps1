$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

$dockerExe = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerExe) {
    Write-Error 'Docker CLI was not found on PATH. Install Docker Desktop or add docker.exe to PATH before running this script.'
    exit 1
}

function Wait-DockerReady {
    param(
        [int]$TimeoutSeconds = 180
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            docker info | Out-Null
            return
        }
        catch {
            Write-Host 'Docker daemon not ready yet; waiting...'
            Start-Sleep -Seconds 5
        }
    }

    Write-Error 'Docker daemon did not become ready within the timeout window.'
    exit 1
}

$dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
if (-not (Test-Path $dockerDesktop)) {
    Write-Host 'Docker Desktop executable not found at default install path; continuing with docker CLI health check.'
}
else {
    try {
        docker info | Out-Null
    }
    catch {
        Write-Host 'Attempting to start Docker Desktop...'
        Start-Process $dockerDesktop
    }
}

Wait-DockerReady

Write-Host 'Docker is ready. Starting options trading stack...'
docker compose -f docker-compose.yml --project-directory . up --build -d

if ($LASTEXITCODE -ne 0) {
    Write-Error 'docker compose up failed.'
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host 'Dashboard and worker services are starting.'
Write-Host 'To inspect status:'
Write-Host '  docker compose ps'
Write-Host 'To inspect logs:'
Write-Host '  docker compose logs -f options-dashboard'
Write-Host '  docker compose logs -f options-worker'
Write-Host 'Open URL: http://localhost:8503'
