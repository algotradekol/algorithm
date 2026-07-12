param(
    [int]$Port = 8000,
    [switch]$Reload
)

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidates = @(
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $repoRoot "backend\venv\Scripts\python.exe")
)

$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    throw "No supported Python environment found. Expected .venv or backend\venv."
}

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    throw "Port $Port is already in use by PID $($listener.OwningProcess). Stop that process first or choose another port."
}

$args = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) {
    $args += "--reload"
}

Push-Location $repoRoot
try {
    & $python @args
} finally {
    Pop-Location
}
