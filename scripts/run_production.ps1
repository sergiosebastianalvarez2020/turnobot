$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

& "$projectRoot\venv\Scripts\python.exe" "$projectRoot\wsgi.py"
