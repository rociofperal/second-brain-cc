# Installs the Claude Code integration on Windows: skills and agents, the hooks, and (with your yes) the
# recommended settings. Run the core bootstrap first (bootstrap.ps1), then:
#     powershell -ExecutionPolicy Bypass -File integrations\claude-code\install.ps1
# Does what install.sh does on macOS and Linux; the hooks are written as
#     "<python.exe>" -X utf8 "<vault>\_bin\<script>.py"
$ErrorActionPreference = 'Stop'
$vault = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $vault '_bin\find-python.ps1')
$py = Find-BrainPython
$env:BRAIN_VAULT = $vault
$env:BRAIN_PYTHON = $py
& $py -X utf8 (Join-Path $PSScriptRoot 'install.py') @args
exit $LASTEXITCODE
