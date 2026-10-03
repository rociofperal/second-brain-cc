# The first run on Windows: asks, one step at a time, whether to connect each optional piece (a KeePass
# database, Google accounts, coordinating with other machines over a shared folder, alert email, the MCP
# server, scheduled jobs through Task Scheduler, the Remote Control server, CLI-agent routines), and where
# to keep files, the one required step. Nothing is installed without a yes; answers are remembered, so it
# is safe to re-run. Does what setup.sh does on macOS and Linux.
#
#   powershell -File integrations\first-run\setup.ps1            ask what is left
#   powershell -File integrations\first-run\setup.ps1 --dry-run  show what would be installed, save nothing
$ErrorActionPreference = 'Stop'
$vault = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if ($env:BRAIN_VAULT) { $vault = $env:BRAIN_VAULT }

if ([Console]::IsInputRedirected) {
    Write-Output 'The first run asks questions and needs a terminal. Run it from one:'
    Write-Output "  powershell -File $vault\integrations\first-run\setup.ps1"
    Write-Output 'Unattended machines and CI can set up the default files directory and decline the rest instead:'
    Write-Output "  python -X utf8 $vault\integrations\first-run\first_run.py skip-all"
    exit 0
}

. (Join-Path $vault '_bin\find-python.ps1')
$py = Find-BrainPython
$env:BRAIN_VAULT = $vault
& $py -X utf8 (Join-Path $vault 'integrations\first-run\first_run.py') run @args
exit $LASTEXITCODE
