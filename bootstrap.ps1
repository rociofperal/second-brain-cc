# Core bootstrap for the Second Brain on Windows: finds Python, then runs bootstrap.py in UTF-8 mode.
# Does what bootstrap.sh does on macOS and Linux.
#
#   git clone <this-repo> $HOME\Brain
#   powershell -ExecutionPolicy Bypass -File $HOME\Brain\bootstrap.ps1
#
# Needs Python 3.9+ (winget install Python.Python.3.12) and Git for Windows. Arguments are passed on.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_bin\find-python.ps1')
$py = Find-BrainPython
$env:BRAIN_VAULT = $PSScriptRoot
$env:BRAIN_PYTHON = $py
& $py -X utf8 (Join-Path $PSScriptRoot 'bootstrap.py') @args
exit $LASTEXITCODE
