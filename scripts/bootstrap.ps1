$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path -LiteralPath $uv)) {
    $installer = Invoke-RestMethod https://astral.sh/uv/0.12.17/install.ps1
    Invoke-Expression $installer
}

& $uv python install `
    --install-dir ".uv-python" `
    --cache-dir ".uv-cache" `
    --no-bin `
    --no-registry `
    3.13.15
if ($LASTEXITCODE -ne 0) { throw "uv python install failed" }

$python = Get-ChildItem -Path ".uv-python" -Filter python.exe -Recurse |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $python) {
    throw "Python 3.13.15 executable was not found under .uv-python"
}

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & $uv venv --python $python
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
}
& $uv sync --extra dev
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
Write-Output "DevContext Python environment is ready."
