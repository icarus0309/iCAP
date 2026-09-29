$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path 'backend/.venv/Scripts/python.exe')) {
    py -3 -m venv backend/.venv
}
& 'backend/.venv/Scripts/python.exe' -m pip install -r 'backend/requirements.txt'
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies failed to install' }
if (-not (Test-Path 'frontend/dist/index.html')) {
    Push-Location frontend
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
    } finally { Pop-Location }
}
$bindHost = if ($env:INNOVATION_HOST) { $env:INNOVATION_HOST } else { '127.0.0.1' }
$bindPort = if ($env:INNOVATION_PORT) { $env:INNOVATION_PORT } else { '8081' }
Set-Location backend
& '.venv/Scripts/python.exe' -m uvicorn app.main:app --host $bindHost --port $bindPort
