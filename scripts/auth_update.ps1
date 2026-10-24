$ErrorActionPreference = "Stop"

. ./scripts/load_python_env.ps1

$venvPythonPath = "./.venv/scripts/python.exe"
if (Test-Path -Path "/usr") {
  # fallback to Linux venv path
  $venvPythonPath = "./.venv/bin/python"
}

& $venvPythonPath "./scripts/auth_update.py"
exit $LASTEXITCODE
