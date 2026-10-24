$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command python3 -ErrorAction Stop
}
& $pythonCmd.Source -m venv .venv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$venvPythonPath = Join-Path (Get-Location) ".venv/Scripts/python.exe"
if (Test-Path -Path "/usr") {
    $venvPythonPath = Join-Path (Get-Location) ".venv/bin/python"
}
& $venvPythonPath -m pip install -r app/backend/requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvPythonPath -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Set-Location app/frontend
npm ci
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Set-Location ../backend
& $venvPythonPath -m quart --app main:app run --port 50505 --host localhost --reload
exit $LASTEXITCODE
